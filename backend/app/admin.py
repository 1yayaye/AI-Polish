"""管理端（运营商后台）API：登录认证、卡密批量生成、用户列表。

认证模型：管理凭据只来自 .env（ADR-0005，改凭据 = 改文件 + 重启，不存在
后台改密码的路径）。登录成功签发 HS256 JWT 存入 HttpOnly cookie，后续请求
凭该 cookie 识别运营商；无 cookie / cookie 无效 / 未配置凭据一律 401，
不区分失败原因（不向探测者泄露信息差异，与卡密认证同一纪律）。

密码与卡密同一条红线：只经 POST body 传输，绝不进 URL/query——进 URL 的
参数会落入 Nginx/访问日志（rewrite-plan §5）。凭据比较用常数时间比较，
防止时序侧信道逐字节猜密码。
"""

import secrets
import time
from typing import Annotated

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import User

router = APIRouter(prefix="/api/admin")

_COOKIE_NAME = "admin_session"
_TOKEN_TTL_SECONDS = 12 * 3600  # 12 小时后需重新登录；无"记住我"等旁路


def _create_token(username: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": username, "iat": now, "exp": now + _TOKEN_TTL_SECONDS},
        settings.jwt_secret,
        algorithm="HS256",
    )


def get_current_admin(
    admin_session: Annotated[str | None, Cookie(alias=_COOKIE_NAME)] = None,
) -> str:
    """校验会话 cookie，返回运营商用户名；缺失或无效一律 401。"""
    if not admin_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        payload = jwt.decode(
            admin_session, settings.jwt_secret, algorithms=["HS256"]
        )
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="未登录") from None
    username = payload.get("sub")
    if not isinstance(username, str):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="未登录")
    return username


def _credentials_configured() -> bool:
    return bool(settings.admin_username and settings.admin_password and settings.jwt_secret)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    username: str


@router.post("/login")
def login(body: LoginRequest, response: Response) -> LoginResponse:
    if not _credentials_configured():
        # 未配置凭据与"密码错误"不可区分：管理端没准备好就等于谁都进不来
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    username_ok = secrets.compare_digest(
        body.username.encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        body.password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )
    if not (username_ok and password_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    response.set_cookie(
        _COOKIE_NAME,
        _create_token(body.username),
        httponly=True,
        samesite="lax",
        max_age=_TOKEN_TTL_SECONDS,
    )
    return LoginResponse(username=body.username)


class CardKeyBatchRequest(BaseModel):
    """批量发卡参数：数量与初始双轨额度（余额分 + 次数上限），次数已用归零。"""

    count: int = Field(ge=1, le=500)
    balance_fen: int = Field(ge=0)
    usage_limit: int = Field(ge=0)


class IssuedCardKey(BaseModel):
    card_key: str
    balance_fen: int
    usage_limit: int


class CardKeyBatchResponse(BaseModel):
    card_keys: list[IssuedCardKey]


def _generate_card_key(session: Session) -> str:
    """生成全局唯一卡密。token_urlsafe(18) ≈ 144 位熵，撞库概率可忽略；
    唯一性仍逐把校验（DB 有 unique 约束兜底），循环直到不冲突。"""
    while True:
        card_key = secrets.token_urlsafe(18)
        if session.execute(select(User.id).where(User.card_key == card_key)).first() is None:
            return card_key


@router.post("/card-keys", status_code=status.HTTP_201_CREATED)
def create_card_keys(
    body: CardKeyBatchRequest,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> CardKeyBatchResponse:
    users = [
        User(
            card_key=_generate_card_key(session),
            balance_fen=body.balance_fen,
            usage_limit=body.usage_limit,
            usage_used=0,
        )
        for _ in range(body.count)
    ]
    session.add_all(users)
    session.flush()  # 生成主键后回读落库值
    return CardKeyBatchResponse(
        card_keys=[
            IssuedCardKey(card_key=u.card_key, balance_fen=u.balance_fen, usage_limit=u.usage_limit)
            for u in users
        ]
    )


class AdminUserSummary(BaseModel):
    """用户列表行：卡密脱敏展示（运营商发卡时已拿到完整卡密，列表不必再露全量）。"""

    id: int
    card_key: str
    balance_fen: int
    usage_limit: int
    usage_remaining: int


def _mask_card_key(card_key: str) -> str:
    if len(card_key) <= 8:
        return card_key[:2] + "****"
    return card_key[:4] + "****" + card_key[-4:]


@router.get("/users")
def list_users(
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> list[AdminUserSummary]:
    users = session.execute(select(User).order_by(User.id)).scalars().all()
    return [
        AdminUserSummary(
            id=u.id,
            card_key=_mask_card_key(u.card_key),
            balance_fen=u.balance_fen,
            usage_limit=u.usage_limit,
            usage_remaining=u.usage_remaining,
        )
        for u in users
    ]
