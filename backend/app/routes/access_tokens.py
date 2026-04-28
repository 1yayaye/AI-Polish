"""Short-lived resource access token endpoints."""

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import OptimizationSession, User
from app.utils.auth import create_scoped_access_token
from app.word_formatter.services import get_job_manager

router = APIRouter(tags=["access"])

ACCESS_TOKEN_TTL_SECONDS = 120


class AccessTokenRequest(BaseModel):
    resource_type: Literal["optimization_session", "word_job"]
    resource_id: str
    action: Literal["stream", "download"]


class AccessTokenResponse(BaseModel):
    access_token: str
    expires_at: datetime


def get_card_key_user(
    x_card_key: str = Header(..., alias="X-Card-Key"),
    db: Session = Depends(get_db),
) -> User:
    user = db.query(User).filter(
        User.card_key == x_card_key,
        User.is_active.is_(True),
    ).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid card key")
    return user


def _assert_resource_owned(
    db: Session,
    *,
    user: User,
    resource_type: str,
    resource_id: str,
    action: str,
) -> None:
    if resource_type == "optimization_session":
        if action != "stream":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported action")
        session = db.query(OptimizationSession).filter(
            OptimizationSession.session_id == resource_id,
            OptimizationSession.user_id == user.id,
        ).first()
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
        return

    if resource_type == "word_job":
        job = get_job_manager().get_job(resource_id)
        if not job or job.user_id != str(user.id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
        return

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported resource type")


@router.post("/access-tokens", response_model=AccessTokenResponse)
async def create_access_token_for_resource(
    request: AccessTokenRequest,
    user: User = Depends(get_card_key_user),
    db: Session = Depends(get_db),
) -> AccessTokenResponse:
    _assert_resource_owned(
        db,
        user=user,
        resource_type=request.resource_type,
        resource_id=request.resource_id,
        action=request.action,
    )
    expires_delta = timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS)
    expires_at = datetime.utcnow() + expires_delta
    token = create_scoped_access_token(
        user_id=user.id,
        resource_type=request.resource_type,
        resource_id=request.resource_id,
        action=request.action,
        expires_delta=expires_delta,
    )
    return AccessTokenResponse(access_token=token, expires_at=expires_at)
