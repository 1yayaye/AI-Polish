"""Usage endpoints for the word formatter API."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import User

from .dependencies import get_current_user
from .schemas import UsageInfoResponse

router = APIRouter()


@router.get("/usage", response_model=UsageInfoResponse)
async def get_usage_info(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get user's usage information (shared with polishing)."""

    usage_limit = user.usage_limit if user.usage_limit is not None else settings.DEFAULT_USAGE_LIMIT
    usage_count = user.usage_count or 0
    remaining = max(0, usage_limit - usage_count) if usage_limit > 0 else -1

    return UsageInfoResponse(
        usage_count=usage_count,
        usage_limit=usage_limit,
        remaining=remaining,
    )


__all__ = ["get_usage_info", "router"]
