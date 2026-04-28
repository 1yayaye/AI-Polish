from typing import Optional

from fastapi import Header, HTTPException, status
import bcrypt

from app.config import settings
from app.utils.auth import verify_token

# 缓存管理员密码的 bcrypt 哈希，避免每次请求都重新哈希
_admin_password_hash: Optional[str] = None


def verify_admin_credentials(username: str, password: str) -> bool:
    global _admin_password_hash
    if username != settings.ADMIN_USERNAME:
        return False

    if _admin_password_hash is None:
        stored = settings.ADMIN_PASSWORD
        # 检查存储的值是否已经是 bcrypt 哈希
        if stored.startswith("$2") and len(stored) == 60:
            _admin_password_hash = stored
        else:
            # 明文密码：哈希并缓存
            _admin_password_hash = bcrypt.hashpw(stored.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    return bcrypt.checkpw(password.encode("utf-8"), _admin_password_hash.encode("utf-8"))


def verify_admin_token(token: str) -> bool:
    payload = verify_token(token)
    if not payload:
        return False
    return payload.get("sub") == settings.ADMIN_USERNAME and payload.get("role") == "admin"


def get_admin_from_token(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少认证令牌")

    token = authorization.split(" ")[1]
    if not verify_admin_token(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期")
    return token
