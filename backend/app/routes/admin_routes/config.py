import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import reload_settings, settings
from app.services.concurrency import concurrency_manager

from .dependencies import get_admin_from_token

router = APIRouter()


def _mask_api_key(key: Optional[str]) -> str:
    """掩码 API 密钥，仅显示前4位和后4位"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"


@router.get("/config")
async def get_config(_: str = Depends(get_admin_from_token)) -> Dict[str, Any]:
    return {
        "compression": {
            "model": settings.COMPRESSION_MODEL,
            "api_key": _mask_api_key(settings.COMPRESSION_API_KEY),
            "base_url": settings.COMPRESSION_BASE_URL or "",
        },
        "thinking": {
            "enabled": settings.THINKING_MODE_ENABLED,
            "effort": settings.THINKING_MODE_EFFORT,
        },
        "system": {
            "deployment_profile": settings.DEPLOYMENT_PROFILE,
            "max_concurrent_users": settings.MAX_CONCURRENT_USERS,
            "word_formatter_max_concurrent_jobs": settings.WORD_FORMATTER_MAX_CONCURRENT_JOBS,
            "word_formatter_job_retention_hours": settings.WORD_FORMATTER_JOB_RETENTION_HOURS,
            "min_free_memory_mb": settings.MIN_FREE_MEMORY_MB,
            "history_compression_threshold": settings.HISTORY_COMPRESSION_THRESHOLD,
            "default_usage_limit": settings.DEFAULT_USAGE_LIMIT,
            "workspace_price_per_10k_cents": settings.WORKSPACE_PRICE_PER_10K_CENTS,
            "segment_skip_threshold": settings.SEGMENT_SKIP_THRESHOLD,
            "use_streaming": settings.USE_STREAMING,
            "max_upload_file_size_mb": settings.MAX_UPLOAD_FILE_SIZE_MB,
            "max_text_input_chars": settings.MAX_TEXT_INPUT_CHARS,
            "api_request_interval": settings.API_REQUEST_INTERVAL,
            "ai_debug_logging": settings.AI_DEBUG_LOGGING,
            "uvicorn_access_log": settings.UVICORN_ACCESS_LOG,
        },
    }


@router.post("/config")
async def update_config(
    updates: Dict[str, str],
    _: str = Depends(get_admin_from_token),
) -> Dict[str, Any]:
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少更新内容")

    # 使用 config.py 中的函数获取 .env 路径，支持 exe 环境
    from app.config import get_env_file_path
    env_path = get_env_file_path()

    if not os.path.exists(env_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f".env 文件不存在: {env_path}")

    with open(env_path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()

    updated_keys = set()
    new_lines: List[str] = []
    for line in lines:
        stripped = line.rstrip("\n")
        if "=" in stripped and not stripped.strip().startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.add(key)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}\n")

    with open(env_path, "w", encoding="utf-8") as handle:
        handle.writelines(new_lines)

    reload_settings()

    if "MAX_CONCURRENT_USERS" in updates:
        try:
            await concurrency_manager.update_limit(int(updates["MAX_CONCURRENT_USERS"]))
        except ValueError:
            pass

    return {"message": "配置已更新并保存", "updated_keys": list(updates.keys())}
