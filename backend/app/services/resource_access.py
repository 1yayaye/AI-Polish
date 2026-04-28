"""Resource-scoped access checks for URL-only transports."""

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.models import OptimizationSession, User
from app.utils.auth import verify_scoped_access_token
from app.word_formatter.services import get_job_manager


def authorize_resource_access(
    db: Session,
    *,
    access_token: str,
    resource_type: str,
    resource_id: str,
    action: str,
) -> User:
    payload = verify_scoped_access_token(access_token, resource_type, resource_id, action)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token")

    user = db.query(User).filter(
        User.id == payload["user_id"],
        User.is_active.is_(True),
    ).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token")

    if resource_type == "optimization_session":
        session = db.query(OptimizationSession).filter(
            OptimizationSession.session_id == resource_id,
            OptimizationSession.user_id == user.id,
        ).first()
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
        return user

    if resource_type == "word_job":
        job = get_job_manager().get_job(resource_id)
        if not job or job.user_id != str(user.id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
        return user

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported resource type")
