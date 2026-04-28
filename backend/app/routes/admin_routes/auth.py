import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import User
from app.services.rate_limiter import client_ip, rate_limiter
from app.utils.auth import create_access_token

from .dependencies import get_admin_from_token, verify_admin_credentials

router = APIRouter()


class AdminLogin(BaseModel):
    username: str
    password: str


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


class CardKeyVerify(BaseModel):
    card_key: str


@router.post("/login", response_model=AdminLoginResponse)
async def admin_login(credentials: AdminLogin, request: Request) -> AdminLoginResponse:
    rate_limiter.check_or_raise(
        f"admin-login:{client_ip(request)}:{credentials.username.lower()}",
        limit=5,
        window_seconds=60,
    )
    if not verify_admin_credentials(credentials.username, credentials.password):
        await asyncio.sleep(0.25)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": credentials.username, "role": "admin"},
        expires_delta=access_token_expires,
    )
    return AdminLoginResponse(access_token=access_token, username=credentials.username)


@router.post("/verify-token")
async def verify_admin_token_endpoint(authorization: Optional[str] = Header(None)) -> Dict[str, bool]:
    get_admin_from_token(authorization)
    return {"valid": True}


@router.post("/verify-card-key")
async def verify_card_key(
    data: CardKeyVerify,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    key_prefix = (data.card_key or "")[:8]
    rate_limiter.check_or_raise(
        f"card-verify:{client_ip(request)}:{key_prefix}",
        limit=10,
        window_seconds=60,
    )
    user = db.query(User).filter(User.card_key == data.card_key, User.is_active.is_(True)).first()
    if not user:
        await asyncio.sleep(0.1)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid card key")

    user.last_used = datetime.utcnow()
    db.commit()
    return {"valid": True, "user_id": user.id, "created_at": user.created_at}
