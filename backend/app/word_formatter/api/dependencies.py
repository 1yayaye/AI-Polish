"""Dependency helpers for word formatter endpoints."""

from datetime import datetime
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, SessionLocal
from app.models.models import User
from app.services.ai_service import AIService
from app.services.usage_service import release_usage_reservation, reserve_usage_or_raise


def get_current_user(
    x_card_key: Optional[str] = Header(None, alias="X-Card-Key"),
    db: Session = Depends(get_db),
) -> User:
    """Authenticate normal JSON/form APIs with X-Card-Key only."""
    if not x_card_key:
        raise HTTPException(status_code=401, detail="Missing X-Card-Key header")

    user = db.query(User).filter(
        User.card_key == x_card_key,
        User.is_active.is_(True),
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid card key")

    user.last_used = datetime.utcnow()
    db.commit()
    return user


def check_usage_limit(user: User) -> None:
    """Check if user has remaining usage quota without reserving it."""
    usage_limit = user.usage_limit if user.usage_limit is not None else settings.DEFAULT_USAGE_LIMIT
    usage_count = user.usage_count or 0

    if usage_limit > 0 and usage_count >= usage_limit:
        raise HTTPException(status_code=403, detail="Usage limit reached")


def reserve_usage(user: User, db: Session) -> None:
    """Atomically reserve one usage unit for a newly created job."""
    reserve_usage_or_raise(db, user=user)


def release_usage(user: User, db: Session) -> None:
    """Release one previously reserved usage unit."""
    release_usage_reservation(db, user=user)


def release_usage_by_user_id(user_id: int) -> None:
    """Release one usage reservation from a background task-owned session."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            release_usage(user, db)
            db.commit()
    finally:
        db.close()


def increment_usage(user: User, db: Session) -> None:
    """Backward-compatible reservation wrapper for legacy imports."""
    reserve_usage(user, db)
    db.commit()


def get_ai_service() -> AIService:
    """Get AI service instance for word formatting."""
    return AIService(
        model="gpt-4o",
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_BASE_URL,
    )


__all__ = [
    "check_usage_limit",
    "get_ai_service",
    "get_current_user",
    "increment_usage",
    "release_usage",
    "release_usage_by_user_id",
    "reserve_usage",
]
