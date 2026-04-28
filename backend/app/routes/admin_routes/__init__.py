from fastapi import APIRouter

from . import auth, config, database, model_profiles, sessions, users
from .config import _mask_api_key
from .database import ALLOWED_TABLES, _model_to_dict
from .dependencies import get_admin_from_token, verify_admin_credentials, verify_admin_token

router = APIRouter(prefix="/admin", tags=["admin"])
router.include_router(auth.router)
router.include_router(users.router)
router.include_router(sessions.router)
router.include_router(config.router)
router.include_router(database.router)
router.include_router(model_profiles.router)

__all__ = [
    "ALLOWED_TABLES",
    "_mask_api_key",
    "_model_to_dict",
    "get_admin_from_token",
    "router",
    "verify_admin_credentials",
    "verify_admin_token",
]
