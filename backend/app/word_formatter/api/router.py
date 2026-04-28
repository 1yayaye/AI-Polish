"""Aggregate router for the word formatter API."""

from fastapi import APIRouter

from . import format, format_check, jobs, preprocess, specs, usage

router = APIRouter(prefix="/word-formatter", tags=["word-formatter"])
router.include_router(usage.router)
router.include_router(specs.router)
router.include_router(format.router)
router.include_router(jobs.router)
router.include_router(preprocess.router)
router.include_router(format_check.router)

__all__ = ["router"]
