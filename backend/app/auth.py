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

CARD_KEY_HEADER = "X-Card-Key"


class InvalidCardKey(Exception):
    """卡密缺失或无效（对外语义一律 401，且不区分两种失败——探测者无法从
    响应体区分失败类型，见 :func:`get_current_user`）。"""


def authenticate_card_key(session: Session, card_key: str | None) -> User:
    """卡密 → 用户（卡密认证的唯一事实）。

    拆成非依赖形态供"需要在外部 I/O 前关闭一切数据库事务"的路径（如 AI
    同步生成端点）在自管短事务内完成认证——请求会话式的
    :func:`get_current_user` 在端点 await 期间无法关闭。返回的 User 是已
    加载的只读快照，事务关闭后取 id 等列值仍可用。缺失或无效抛
    :class:`InvalidCardKey`，由 HTTP 层映射 401。
    """
    # 缺失与无效共用同一 detail：探测者无法从响应体区分失败类型（见模块级 docstring）
    if not card_key:
        raise InvalidCardKey("卡密缺失或无效")
    user = session.execute(
        select(User).where(User.card_key == card_key)
    ).scalar_one_or_none()
    if user is None:
        raise InvalidCardKey("卡密缺失或无效")
    return user


def get_current_user(
    card_key: Annotated[str | None, Header(alias=CARD_KEY_HEADER)] = None,
    session: Session = Depends(get_session),
) -> User:
    """从 X-Card-Key header 解析当前用户；缺失或无效一律 401。"""
    try:
        return authenticate_card_key(session, card_key)
    except InvalidCardKey as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
