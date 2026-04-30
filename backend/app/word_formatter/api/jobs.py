"""Job status, streaming, download, and report endpoints."""

import io
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.database import get_db
from app.models.models import User
from app.services.resource_access import authorize_resource_access

from ..services import JobStatus, get_job_manager
from .dependencies import get_current_user
from .schemas import JobStatusResponse

router = APIRouter()


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get job status and progress."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.user_id != str(user.id):
        raise HTTPException(status_code=403, detail="Forbidden")

    progress = job.current_progress
    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        progress=progress.progress if progress else None,
        phase=progress.phase if progress else None,
        message=progress.message if progress else None,
        error=job.error,
        output_filename=job.output_filename,
    )


@router.get("/jobs/{job_id}/stream")
async def stream_job_progress(
    job_id: str,
    request: Request,
    access_token: str = Query(...),
    db: Session = Depends(get_db),
):
    """Stream job progress via SSE."""
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

    async def event_generator():
        async for event in job_manager.stream_progress(job_id):
            if await request.is_disconnected():
                break

            event_type = event.get("event", "message")
            data = json.dumps(event.get("data", {}), ensure_ascii=False)
            yield f"event: {event_type}\ndata: {data}\n\n"

    return EventSourceResponse(event_generator())


@router.get("/jobs/{job_id}/download")
async def download_result(
    job_id: str,
    access_token: str = Query(...),
    db: Session = Depends(get_db),
):
    """Download the formatted document."""
    authorize_resource_access(
        db,
        access_token=access_token,
        resource_type="word_job",
        resource_id=job_id,
        action="download",
    )

    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Job is not complete")

    if not job.output_bytes:
        raise HTTPException(status_code=500, detail="Output file missing")

    filename = job.output_filename or "formatted.docx"
    encoded_filename = quote(filename, safe="")

    try:
        filename.encode("ascii")
        ascii_fallback = filename
    except UnicodeEncodeError:
        ascii_fallback = "download.docx"

    data = job.output_bytes

    def stream_and_release():
        yield data
        job.output_bytes = None

    return StreamingResponse(
        stream_and_release(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded_filename}",
        },
    )


@router.get("/jobs/{job_id}/report")
async def get_validation_report(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the validation report for a completed job."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.user_id != str(user.id):
        raise HTTPException(status_code=403, detail="Forbidden")

    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Job is not complete")

    if not job.result or not job.result.report:
        return {"report": None}

    report = job.result.report
    return {
        "report": {
            "summary": {
                "ok": report.summary.ok,
                "errors": report.summary.errors,
                "warnings": report.summary.warnings,
                "infos": report.summary.infos,
            },
            "violations": [
                {
                    "id": v.violation_id,
                    "severity": v.severity,
                    "message": v.message,
                    "location": v.location.model_dump() if v.location else None,
                }
                for v in report.violations[:50]
            ],
        },
    }


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a job and its data."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.user_id != str(user.id):
        raise HTTPException(status_code=403, detail="Forbidden")

    job_manager.delete_job(job_id)

    return {"message": "Job deleted"}


@router.get("/jobs")
async def list_jobs(
    limit: int = Query(10, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List user's recent jobs."""
    job_manager = get_job_manager()
    jobs = job_manager.get_user_jobs(str(user.id), limit)

    return {
        "jobs": [
            {
                "job_id": j.job_id,
                "job_type": j.job_type.value,
                "status": j.status.value,
                "input_file_name": j.input_file_name,
                "output_filename": j.output_filename,
                "created_at": j.created_at.isoformat(),
                "updated_at": j.updated_at.isoformat(),
            }
            for j in jobs
        ]
    }


__all__ = [
    "delete_job",
    "download_result",
    "get_job_status",
    "get_validation_report",
    "list_jobs",
    "router",
    "stream_job_progress",
]
