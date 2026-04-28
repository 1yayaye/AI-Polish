import secrets
import string
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Header, HTTPException, status
import bcrypt
import jwt
from jwt.exceptions import InvalidTokenError
from sqlalchemy.orm import Session

from app.config import settings


def generate_card_key(length: int = 16, prefix: str = "") -> str:
    """生成卡密"""
    chars = string.ascii_uppercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(length))
    if prefix:
        return f"{prefix}-{random_part}"
    return random_part


def generate_access_link(card_key: str, base_url: str = "http://localhost:9800") -> str:
    """生成访问链接"""
    return f"{base_url}/access/{card_key}"


def generate_session_id() -> str:
    """生成会话ID"""
    return secrets.token_urlsafe(32)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def get_password_hash(password: str) -> str:
    """哈希密码"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建访问令牌"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def create_scoped_access_token(
    *,
    user_id: int,
    resource_type: str,
    resource_id: str,
    action: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a short-lived token bound to one resource action."""
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=2))
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "resource_type": resource_type,
        "resource_id": str(resource_id),
        "action": action,
        "scope": "resource_access",
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """验证令牌"""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except InvalidTokenError:
        return None


def verify_scoped_access_token(
    token: str,
    resource_type: str,
    resource_id: str,
    action: str,
) -> Optional[dict]:
    """Verify a short-lived token is scoped to the requested resource action."""
    payload = verify_token(token)
    if not payload:
        return None
    if payload.get("scope") != "resource_access":
        return None
    if payload.get("resource_type") != resource_type:
        return None
    if str(payload.get("resource_id")) != str(resource_id):
        return None
    if payload.get("action") != action:
        return None
    return payload


def get_current_user(
    x_card_key: Optional[str] = Header(None, alias="X-Card-Key"),
    db: Session = None,
):
    """从 X-Card-Key header 获取当前用户"""
    from app.database import get_db as _get_db
    from app.models.models import User

    if not x_card_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 X-Card-Key 请求头",
        )

    if db is None:
        db = next(_get_db())
        close_db = True
    else:
        close_db = False

    try:
        user = db.query(User).filter(
            User.card_key == x_card_key,
            User.is_active.is_(True),
        ).first()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的卡密",
            )

        user.last_used = datetime.utcnow()
        db.commit()
        return user
    finally:
        if close_db:
            db.close()
