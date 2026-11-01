"""业务 API 路由（/api 前缀，由 Nginx 反代，见 main.py 路由约定）。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/api")


class QuotaResponse(BaseModel):
    """本人双轨额度：余额（分）与剩余次数，两轨相互独立（GLOSSARY：余额/用量上限）。"""

    balance_fen: int
    usage_remaining: int


@router.get("/me/quota")
def my_quota(user: User = Depends(get_current_user)) -> QuotaResponse:
    return QuotaResponse(balance_fen=user.balance_fen, usage_remaining=user.usage_remaining)
