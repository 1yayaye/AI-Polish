"""Text preprocessing endpoints."""

import json

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.database import get_db
from app.models.models import User
from app.services.resource_guard import ensure_memory_available
from app.services.resource_access import authorize_resource_access

from ..services import JobStatus, JobType, PreprocessConfig, get_job_manager
from ..utils.docx_text import extract_text_from_docx
from .dependencies import get_ai_service, get_current_user, release_usage_by_user_id, reserve_usage
from .schemas import (
    ParagraphInfoResponse,
    PreprocessJobResponse,
    PreprocessRequest,
    PreprocessResultResponse,
)
from .upload_limits import read_limited_upload, validate_text_size

router = APIRouter()


@router.post("/preprocess/text", response_model=PreprocessJobResponse)
async def preprocess_text(
    request: PreprocessRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start text preprocessing job."""
    if not request.text:
        raise HTTPException(status_code=400, detail="Text content cannot be empty")
    validate_text_size(request.text)
    ensure_memory_available("word formatter preprocess text job")

    preprocess_config = PreprocessConfig(
        chunk_paragraphs=request.chunk_paragraphs,
        chunk_chars=request.chunk_chars,
    )

    reserve_usage(user, db)
    db.commit()

    job_manager = get_job_manager()
    job = job_manager.create_job(
        job_type=JobType.PREPROCESS,
        user_id=str(user.id),
        input_text=request.text,
        preprocess_config=preprocess_config,
    )

    ai_service = get_ai_service()

    async def run_job():
        result = await job_manager.run_job(job.job_id, ai_service)
        if result.status != JobStatus.COMPLETED:
            release_usage_by_user_id(user.id)

    background_tasks.add_task(run_job)

    return PreprocessJobResponse(
        job_id=job.job_id,
        status=job.status.value,
        message="Preprocess job created and processing",
    )


@router.post("/preprocess/file", response_model=PreprocessJobResponse)
async def preprocess_file(
    file: UploadFile = File(...),
    chunk_paragraphs: int = Query(40, ge=10, le=100),
    chunk_chars: int = Query(8000, ge=2000, le=15000),
    background_tasks: BackgroundTasks = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and preprocess a document file."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename cannot be empty")
    ensure_memory_available("word formatter preprocess file job")

    ext = file.filename.lower().rsplit(".", 1)[-1] if "." in file.filename else ""
    if ext not in {"docx", "txt", "md", "markdown"}:
        raise HTTPException(status_code=400, detail="Only .docx, .txt, and .md files are supported")

    content = await read_limited_upload(file)

    if ext == "docx":
        try:
            text = extract_text_from_docx(content)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to parse docx file: {exc}") from exc
    else:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("gbk")
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=400, detail="Failed to decode file content") from exc

    if not text.strip():
        raise HTTPException(status_code=400, detail="File content cannot be empty")
    validate_text_size(text)

    preprocess_config = PreprocessConfig(
        chunk_paragraphs=chunk_paragraphs,
        chunk_chars=chunk_chars,
    )

    reserve_usage(user, db)
    db.commit()

    job_manager = get_job_manager()
    job = job_manager.create_job(
        job_type=JobType.PREPROCESS,
        user_id=str(user.id),
        input_text=text,
        input_file_name=file.filename,
        preprocess_config=preprocess_config,
    )

    ai_service = get_ai_service()

    async def run_job():
        result = await job_manager.run_job(job.job_id, ai_service)
        if result.status != JobStatus.COMPLETED:
            release_usage_by_user_id(user.id)

    background_tasks.add_task(run_job)

    return PreprocessJobResponse(
        job_id=job.job_id,
        status=job.status.value,
        message="File uploaded and preprocess job is running",
    )


@router.get("/preprocess/{job_id}/stream")
async def stream_preprocess_progress(
    job_id: str,
    request: Request,
    access_token: str = Query(...),
    db: Session = Depends(get_db),
):
    """Stream preprocessing progress via SSE."""
    authorize_resource_access(
        db,
        access_token=access_token,
        resource_type="word_job",
        resource_id=job_id,
        action="stream",
    )

    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.job_type != JobType.PREPROCESS:
        raise HTTPException(status_code=400, detail="Job is not a preprocess job")

    async def event_generator():
        async for event in job_manager.stream_progress(job_id):
            if await request.is_disconnected():
                break

            event_type = event.get("event", "message")
            data = json.dumps(event.get("data", {}), ensure_ascii=False)
            yield f"event: {event_type}\ndata: {data}\n\n"

    return EventSourceResponse(event_generator())


@router.get("/preprocess/{job_id}/result", response_model=PreprocessResultResponse)
async def get_preprocess_result(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get preprocessing result."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.user_id != str(user.id):
        raise HTTPException(status_code=403, detail="Forbidden")

    if job.job_type != JobType.PREPROCESS:
        raise HTTPException(status_code=400, detail="Job is not a preprocess job")

    if job.status in {JobStatus.PENDING, JobStatus.RUNNING}:
        raise HTTPException(status_code=400, detail="Job is not complete")

    if job.status == JobStatus.FAILED:
        return PreprocessResultResponse(
            success=False,
            error=job.error,
        )

    result = job.preprocess_result
    if not result:
        raise HTTPException(status_code=500, detail="Preprocess result missing")

    return PreprocessResultResponse(
        success=result.success,
        marked_text=result.marked_text,
        paragraphs=[
            ParagraphInfoResponse(
                index=p.index,
                text=p.text,
                paragraph_type=p.paragraph_type,
                confidence=p.confidence,
                is_rule_identified=p.is_rule_identified,
            )
            for p in result.paragraphs
        ],
        type_statistics=result.type_statistics,
        integrity_check_passed=result.integrity_check_passed,
        warnings=result.warnings,
        error=result.error,
    )


@router.delete("/preprocess/{job_id}")
async def delete_preprocess_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a preprocess job."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.user_id != str(user.id):
        raise HTTPException(status_code=403, detail="Forbidden")

    if job.job_type != JobType.PREPROCESS:
        raise HTTPException(status_code=400, detail="Job is not a preprocess job")

    job_manager.delete_job(job_id)

    return {"message": "Preprocess job deleted"}


__all__ = [
    "delete_preprocess_job",
    "get_preprocess_result",
    "preprocess_file",
    "preprocess_text",
    "router",
    "stream_preprocess_progress",
]
