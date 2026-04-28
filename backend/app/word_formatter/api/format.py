"""Document formatting endpoints."""

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.services.resource_guard import ensure_memory_available

from ..services import CompileOptions, InputFormat, JobStatus, JobType, get_job_manager, validate_custom_spec
from ..utils.docx_text import extract_text_from_docx
from .dependencies import get_current_user, release_usage_by_user_id, reserve_usage
from .schemas import FormatRequest, JobResponse
from .upload_limits import read_limited_upload, validate_text_size

router = APIRouter()


@router.post("/format/text", response_model=JobResponse)
async def format_text(
    request: FormatRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Format text document and return job ID."""
    if not request.text:
        raise HTTPException(status_code=400, detail="Text content cannot be empty")
    validate_text_size(request.text)
    ensure_memory_available("word formatter text job")

    try:
        input_format = InputFormat(request.input_format)
    except ValueError:
        input_format = InputFormat.AUTO

    custom_spec = None
    if request.custom_spec_json:
        try:
            custom_spec = validate_custom_spec(request.custom_spec_json)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid custom spec: {exc}") from exc

    options = CompileOptions(
        input_format=input_format,
        spec_name=request.spec_name,
        custom_spec=custom_spec,
        include_cover=request.include_cover,
        include_toc=request.include_toc,
        toc_title=request.toc_title,
    )

    reserve_usage(user, db)
    db.commit()

    job_manager = get_job_manager()
    job = job_manager.create_job(
        job_type=JobType.FORMAT,
        user_id=str(user.id),
        input_text=request.text,
        options=options,
    )

    async def run_job():
        result = await job_manager.run_job(job.job_id)
        if result.status != JobStatus.COMPLETED:
            release_usage_by_user_id(user.id)

    background_tasks.add_task(run_job)

    return JobResponse(
        job_id=job.job_id,
        status=job.status.value,
        message="Job created and processing",
    )


@router.post("/format/file", response_model=JobResponse)
async def format_file(
    file: UploadFile = File(...),
    input_format: str = Query("auto"),
    spec_name: Optional[str] = Query(None),
    include_cover: bool = Query(True),
    include_toc: bool = Query(True),
    toc_title: str = Query("Contents"),
    background_tasks: BackgroundTasks = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and format a document file (docx, txt, md)."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename cannot be empty")
    ensure_memory_available("word formatter file job")

    ext = file.filename.lower().rsplit(".", 1)[-1] if "." in file.filename else ""
    if ext not in {"docx", "txt", "md", "markdown"}:
        raise HTTPException(status_code=400, detail="Only .docx, .txt, and .md files are supported")

    content = await read_limited_upload(file)

    if ext == "docx":
        try:
            text = extract_text_from_docx(content)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to parse docx file: {exc}") from exc
        detected_format = InputFormat.PLAINTEXT
    else:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("gbk")
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=400, detail="Failed to decode file content") from exc
        detected_format = InputFormat.MARKDOWN if ext in {"md", "markdown"} else InputFormat.AUTO

    if not text.strip():
        raise HTTPException(status_code=400, detail="File content cannot be empty")
    validate_text_size(text)

    try:
        fmt = InputFormat(input_format)
    except ValueError:
        fmt = detected_format

    options = CompileOptions(
        input_format=fmt,
        spec_name=spec_name,
        include_cover=include_cover,
        include_toc=include_toc,
        toc_title=toc_title,
    )

    reserve_usage(user, db)
    db.commit()

    job_manager = get_job_manager()
    job = job_manager.create_job(
        job_type=JobType.FORMAT,
        user_id=str(user.id),
        input_text=text,
        input_file_name=file.filename,
        options=options,
    )

    async def run_job():
        result = await job_manager.run_job(job.job_id)
        if result.status != JobStatus.COMPLETED:
            release_usage_by_user_id(user.id)

    background_tasks.add_task(run_job)

    return JobResponse(
        job_id=job.job_id,
        status=job.status.value,
        message="File uploaded and processing",
    )


__all__ = ["format_file", "format_text", "router"]
