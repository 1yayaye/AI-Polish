from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import OptimizationSegment, OptimizationSession, User
from app.schemas import CardKeyGenerate, CardKeyResponse, UserResponse, UserUsageUpdate
from app.services.billing_service import adjust_user_workspace_balance
from app.utils.auth import generate_access_link, generate_card_key
from app.utils.timezone import china_day_start_utc_naive, china_days_ago_utc_naive
from app.word_formatter.services.job_manager import get_job_manager

from .dependencies import get_admin_from_token, verify_admin_credentials

router = APIRouter()


class CardKeyCreate(BaseModel):
    card_key: Optional[str] = None
    usage_limit: Optional[int] = Field(default=None, ge=0)
    initial_balance_cents: int = Field(default=0, ge=0)


class UserBalanceUpdate(BaseModel):
    delta_cents: int
    reason: Optional[str] = None


@router.post("/users")
@router.post("/card-keys")
async def create_card_key(
    data: CardKeyCreate,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    card_key = data.card_key or generate_card_key()
    existing_user = db.query(User).filter(User.card_key == card_key).first()
    if existing_user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该卡密已存在")

    usage_limit = 0 if data.usage_limit is None else data.usage_limit
    access_link = generate_access_link(card_key)
    user = User(
        card_key=card_key,
        access_link=access_link,
        is_active=True,
        usage_limit=usage_limit,
        usage_count=0,
        workspace_balance_cents=0,
        workspace_total_spent_cents=0,
    )
    db.add(user)
    db.flush()
    if data.initial_balance_cents > 0:
        adjust_user_workspace_balance(
            db,
            user=user,
            delta_cents=data.initial_balance_cents,
            reason="initial card balance",
        )
    db.commit()
    db.refresh(user)
    return {
        "card_key": user.card_key,
        "access_link": user.access_link,
        "usage_limit": user.usage_limit,
        "workspace_balance_cents": user.workspace_balance_cents,
        "workspace_total_spent_cents": user.workspace_total_spent_cents,
        "created_at": user.created_at,
    }


@router.post("/users/batch")
@router.post("/batch-generate-keys")
async def batch_generate_keys(
    count: int,
    prefix: str = "",
    usage_limit: Optional[int] = None,
    initial_balance_cents: int = 0,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    if count <= 0 or count > 100:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="批量生成数量必须在 1-100 之间")

    if initial_balance_cents < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="初始余额不能为负数")

    limit = 0 if usage_limit is None else usage_limit
    results: List[Dict[str, Any]] = []
    for _ in range(count):
        card_key = generate_card_key(prefix=prefix)
        access_link = generate_access_link(card_key)
        user = User(
            card_key=card_key,
            access_link=access_link,
            is_active=True,
            usage_limit=limit,
            usage_count=0,
            workspace_balance_cents=0,
            workspace_total_spent_cents=0,
        )
        db.add(user)
        db.flush()
        if initial_balance_cents > 0:
            adjust_user_workspace_balance(
                db,
                user=user,
                delta_cents=initial_balance_cents,
                reason="initial batch card balance",
            )
        db.commit()
        db.refresh(user)
        results.append(
            {
                "card_key": card_key,
                "access_link": access_link,
                "usage_limit": user.usage_limit,
                "workspace_balance_cents": user.workspace_balance_cents,
                "workspace_total_spent_cents": user.workspace_total_spent_cents,
                "created_at": user.created_at,
            }
        )
    return {"count": len(results), "keys": results}


@router.get("/users", response_model=List[UserResponse])
async def get_all_users(_: str = Depends(get_admin_from_token), db: Session = Depends(get_db)) -> List[User]:
    return db.query(User).order_by(User.created_at.desc()).all()


@router.patch("/users/{user_id}/toggle")
async def toggle_user_status(
    user_id: int,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    user.is_active = not user.is_active
    db.commit()
    db.refresh(user)
    return {
        "id": user.id,
        "card_key": user.card_key,
        "is_active": user.is_active,
        "message": f"用户已{'启用' if user.is_active else '禁用'}",
    }


@router.patch("/users/{user_id}/usage")
async def update_user_usage(
    user_id: int,
    payload: UserUsageUpdate,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    user.usage_limit = payload.usage_limit
    if payload.reset_usage_count:
        user.usage_count = 0
    db.commit()
    db.refresh(user)
    return {
        "id": user.id,
        "usage_limit": user.usage_limit,
        "usage_count": user.usage_count,
        "message": "使用限制已更新",
    }


@router.patch("/users/{user_id}/balance")
async def update_user_balance(
    user_id: int,
    payload: UserBalanceUpdate,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    transaction = adjust_user_workspace_balance(
        db,
        user=user,
        delta_cents=payload.delta_cents,
        reason=payload.reason,
    )
    db.commit()
    db.refresh(user)
    db.refresh(transaction)
    return {
        "id": user.id,
        "workspace_balance_cents": user.workspace_balance_cents,
        "workspace_total_spent_cents": user.workspace_total_spent_cents,
        "transaction": {
            "id": transaction.id,
            "transaction_type": transaction.transaction_type,
            "amount_cents": transaction.amount_cents,
            "balance_after_cents": transaction.balance_after_cents,
            "reason": transaction.reason,
            "created_at": transaction.created_at,
        },
        "message": "余额已更新",
    }


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    db.delete(user)
    db.commit()
    return {"message": "用户已删除", "card_key": user.card_key}


@router.get("/statistics")
async def get_statistics(_: str = Depends(get_admin_from_token), db: Session = Depends(get_db)) -> Dict[str, Any]:
    total_users = db.query(User).count() or 0
    active_users = db.query(User).filter(User.is_active.is_(True)).count() or 0
    inactive_users = total_users - active_users
    used_users = db.query(User).filter(User.last_used.isnot(None)).count() or 0

    total_sessions = db.query(OptimizationSession).count() or 0
    completed_sessions = db.query(OptimizationSession).filter(OptimizationSession.status == "completed").count() or 0
    processing_sessions = db.query(OptimizationSession).filter(OptimizationSession.status == "processing").count() or 0
    queued_sessions = db.query(OptimizationSession).filter(OptimizationSession.status == "queued").count() or 0
    failed_sessions = db.query(OptimizationSession).filter(OptimizationSession.status == "failed").count() or 0

    total_segments = db.query(OptimizationSegment).count() or 0
    completed_segments = db.query(OptimizationSegment).filter(OptimizationSegment.status == "completed").count() or 0

    seven_days_ago = china_days_ago_utc_naive(7)
    recent_active_users = db.query(User).filter(User.last_used >= seven_days_ago).count() or 0

    today_start = china_day_start_utc_naive()
    today_new_users = db.query(User).filter(User.created_at >= today_start).count() or 0
    today_active_users = db.query(User).filter(User.last_used >= today_start).count() or 0
    today_sessions = db.query(OptimizationSession).filter(OptimizationSession.created_at >= today_start).count() or 0

    # 统计文本处理字数
    all_sessions = db.query(OptimizationSession).filter(
        OptimizationSession.status == "completed"
    ).all()

    total_original_chars = sum(len(s.original_text) for s in all_sessions if s.original_text)

    # 统计各处理模式的使用量
    paper_polish_count = db.query(OptimizationSession).filter(
        OptimizationSession.processing_mode == "paper_polish"
    ).count() or 0

    paper_polish_enhance_count = db.query(OptimizationSession).filter(
        OptimizationSession.processing_mode == "paper_polish_enhance"
    ).count() or 0

    emotion_polish_count = db.query(OptimizationSession).filter(
        OptimizationSession.processing_mode == "emotion_polish"
    ).count() or 0

    # 统计平均处理时间
    completed_with_time = db.query(OptimizationSession).filter(
        OptimizationSession.status == "completed",
        OptimizationSession.completed_at.isnot(None),
        OptimizationSession.created_at.isnot(None)
    ).all()

    avg_processing_time = 0
    if completed_with_time:
        total_time = sum(
            (s.completed_at - s.created_at).total_seconds()
            for s in completed_with_time
        )
        avg_processing_time = total_time / len(completed_with_time)

    return {
        "users": {
            "total": total_users,
            "active": active_users,
            "inactive": inactive_users,
            "used": used_users,
            "unused": total_users - used_users,
            "today_new": today_new_users,
            "today_active": today_active_users,
            "recent_active_7days": recent_active_users,
        },
        "sessions": {
            "total": total_sessions,
            "completed": completed_sessions,
            "processing": processing_sessions,
            "queued": queued_sessions,
            "failed": failed_sessions,
            "today": today_sessions,
        },
        "segments": {
            "total": total_segments,
            "completed": completed_segments,
            "pending": total_segments - completed_segments,
        },
        "processing": {
            "total_chars_processed": total_original_chars,
            "avg_processing_time": round(avg_processing_time, 2),
            "paper_polish_count": paper_polish_count,
            "paper_polish_enhance_count": paper_polish_enhance_count,
            "emotion_polish_count": emotion_polish_count,
        },
        "word_formatter": get_job_manager().get_stats(),
    }


@router.get("/users/{user_id}/details")
async def get_user_details(
    user_id: int,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    user_sessions = db.query(OptimizationSession).filter(OptimizationSession.user_id == user_id).all()
    total_sessions = len(user_sessions)
    completed_sessions = sum(1 for session in user_sessions if session.status == "completed")

    session_ids = [session.id for session in user_sessions]
    total_segments = 0
    completed_segments = 0
    if session_ids:
        total_segments = db.query(OptimizationSegment).filter(OptimizationSegment.session_id.in_(session_ids)).count()
        completed_segments = (
            db.query(OptimizationSegment)
            .filter(OptimizationSegment.session_id.in_(session_ids), OptimizationSegment.status == "completed")
            .count()
        )

    recent_sessions = (
        db.query(OptimizationSession)
        .filter(OptimizationSession.user_id == user_id)
        .order_by(OptimizationSession.created_at.desc())
        .limit(5)
        .all()
    )

    return {
        "user": {
            "id": user.id,
            "card_key": user.card_key,
            "is_active": user.is_active,
            "created_at": user.created_at,
            "last_used": user.last_used,
            "usage_limit": user.usage_limit,
            "usage_count": user.usage_count,
            "workspace_balance_cents": user.workspace_balance_cents or 0,
            "workspace_total_spent_cents": user.workspace_total_spent_cents or 0,
        },
        "statistics": {
            "total_sessions": total_sessions,
            "completed_sessions": completed_sessions,
            "processing_sessions": total_sessions - completed_sessions,
            "total_segments": total_segments,
            "completed_segments": completed_segments,
        },
        "recent_sessions": [
            {
                "id": session.id,
                "status": session.status,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
            }
            for session in recent_sessions
        ],
    }


@router.post("/generate-keys", response_model=List[CardKeyResponse])
async def generate_keys(
    data: CardKeyGenerate,
    admin_password: str,
    db: Session = Depends(get_db),
) -> List[CardKeyResponse]:
    if not verify_admin_credentials(settings.ADMIN_USERNAME, admin_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员密码错误")

    results: List[CardKeyResponse] = []
    for _ in range(data.count):
        card_key = generate_card_key(prefix=data.prefix or "")
        access_link = generate_access_link(card_key)
        user = User(
            card_key=card_key,
            access_link=access_link,
            is_active=True,
            usage_limit=settings.DEFAULT_USAGE_LIMIT,
            usage_count=0,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        results.append(
            CardKeyResponse(
                card_key=card_key,
                access_link=access_link,
                created_at=user.created_at,
            )
        )

    return results
