"""卡密认证：卡密只经 X-Card-Key 请求头传递，绝不进 URL。

进 URL 的参数会落入 Nginx/访问日志，等于把用户身份凭证写进日志
（rewrite-plan §5、spec 用户故事 14）；header 不落访问日志，故认证
只认 header。任何路由需要"当前用户"时依赖 get_current_user 即可，
无效/缺失卡密统一 401，不区分两种失败（不向探测者泄露信息差异）。
"""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import User

_CARD_KEY_HEADER = "X-Card-Key"


def get_current_user(
    card_key: Annotated[str | None, Header(alias=_CARD_KEY_HEADER)] = None,
    session: Session = Depends(get_session),
) -> User:
    """从 X-Card-Key header 解析当前用户；缺失或无效一律 401。"""
    if not card_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="缺少卡密，请在 X-Card-Key 请求头中提供",
        )
    user = session.execute(
        select(User).where(User.card_key == card_key)
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="卡密无效",
        )
    return user
