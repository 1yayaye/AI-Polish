"""Atomic usage reservation helpers."""

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.models import User


def reserve_usage(db: Session, *, user: User) -> bool:
    statement = (
        update(User)
        .where(
            User.id == user.id,
            (User.usage_limit == 0) | (User.usage_count < User.usage_limit),
        )
        .values(usage_count=User.usage_count + 1)
    )
    result = db.execute(statement)
    if result.rowcount != 1:
        return False
    db.flush()
    db.refresh(user)
    return True


def reserve_usage_or_raise(db: Session, *, user: User) -> None:
    if not reserve_usage(db, user=user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usage limit reached",
        )


def release_usage_reservation(db: Session, *, user: User) -> bool:
    statement = (
        update(User)
        .where(User.id == user.id, User.usage_count > 0)
        .values(usage_count=User.usage_count - 1)
    )
    result = db.execute(statement)
    if result.rowcount != 1:
        return False
    db.flush()
    db.refresh(user)
    return True
