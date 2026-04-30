import os
import sys
from typing import Optional

from fastapi import HTTPException, status

from app.config import settings


def get_available_memory_mb() -> Optional[int]:
    if sys.platform.startswith("linux"):
        return _linux_available_memory_mb()
    if sys.platform.startswith("win"):
        return _windows_available_memory_mb()
    return None


def ensure_memory_available(operation: str) -> None:
    minimum_mb = max(int(getattr(settings, "MIN_FREE_MEMORY_MB", 0) or 0), 0)
    if minimum_mb <= 0:
        return

    available_mb = get_available_memory_mb()
    if available_mb is None:
        return

    if available_mb < minimum_mb:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Insufficient available memory for {operation}. "
                f"Available: {available_mb} MB, required: {minimum_mb} MB."
            ),
        )


def get_resource_status() -> dict:
    available_mb = get_available_memory_mb()
    minimum_mb = max(int(getattr(settings, "MIN_FREE_MEMORY_MB", 0) or 0), 0)
    memory_ok = True if available_mb is None else available_mb >= minimum_mb

    from app.services.concurrency import concurrency_manager

    return {
        "deployment_profile": settings.DEPLOYMENT_PROFILE,
        "available_memory_mb": available_mb,
        "min_free_memory_mb": minimum_mb,
        "memory_ok": memory_ok,
        "max_concurrent_users": settings.MAX_CONCURRENT_USERS,
        "active_sessions": concurrency_manager.get_active_count(),
        "queue_length": len(concurrency_manager.queue),
        "word_formatter_max_concurrent_jobs": settings.WORD_FORMATTER_MAX_CONCURRENT_JOBS,
        "word_formatter_job_retention_hours": settings.WORD_FORMATTER_JOB_RETENTION_HOURS,
        "max_upload_file_size_mb": settings.MAX_UPLOAD_FILE_SIZE_MB,
        "max_text_input_chars": settings.MAX_TEXT_INPUT_CHARS,
        "ai_debug_logging": settings.AI_DEBUG_LOGGING,
        "uvicorn_access_log": settings.UVICORN_ACCESS_LOG,
    }


def _linux_available_memory_mb() -> Optional[int]:
    meminfo_path = "/proc/meminfo"
    if not os.path.exists(meminfo_path):
        return None

    try:
        with open(meminfo_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024
    except (OSError, ValueError):
        return None
    return None


def _windows_available_memory_mb() -> Optional[int]:
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status_info = MemoryStatus()
        status_info.dwLength = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status_info)):
            return None
        return int(status_info.ullAvailPhys // (1024 * 1024))
    except Exception:
        return None
