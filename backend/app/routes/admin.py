from typing import Any, Dict

from app.routes.admin_routes import router
from app.routes.admin_routes import database as _database
from app.routes.admin_routes import dependencies as _dependencies
from app.routes.admin_routes.config import _mask_api_key
from app.routes.admin_routes.database import ALLOWED_TABLES
from app.routes.admin_routes.dependencies import get_admin_from_token

settings = _dependencies.settings
inspect = _database.inspect
_admin_password_hash = _dependencies._admin_password_hash


def verify_admin_credentials(username: str, password: str) -> bool:
    global _admin_password_hash
    original_settings = _dependencies.settings
    try:
        _dependencies.settings = settings
        _dependencies._admin_password_hash = _admin_password_hash
        result = _dependencies.verify_admin_credentials(username, password)
        _admin_password_hash = _dependencies._admin_password_hash
        return result
    finally:
        _dependencies.settings = original_settings


def verify_admin_token(token: str) -> bool:
    original_settings = _dependencies.settings
    try:
        _dependencies.settings = settings
        return _dependencies.verify_admin_token(token)
    finally:
        _dependencies.settings = original_settings


def _model_to_dict(record: Any) -> Dict[str, Any]:
    original_inspect = _database.inspect
    try:
        _database.inspect = inspect
        return _database._model_to_dict(record)
    finally:
        _database.inspect = original_inspect


__all__ = [
    "ALLOWED_TABLES",
    "_mask_api_key",
    "_model_to_dict",
    "get_admin_from_token",
    "router",
    "verify_admin_credentials",
    "verify_admin_token",
]
