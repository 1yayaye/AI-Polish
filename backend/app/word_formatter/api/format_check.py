"""Format-check endpoints."""

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User

from ..services import PARAGRAPH_TYPES, check_format
from ..utils.docx_text import extract_text_from_docx
from .dependencies import get_current_user
from .schemas import (
    FormatCheckRequest,
    FormatCheckResponse,
    FormatIssueResponse,
    FormatParagraphResponse,
    ParagraphTypesResponse,
)
from .upload_limits import read_limited_upload, validate_text_size

router = APIRouter()


@router.get("/format-check/types", response_model=ParagraphTypesResponse)
async def get_paragraph_types():
    """Get available paragraph types and their descriptions."""
    return ParagraphTypesResponse(types=PARAGRAPH_TYPES)


@router.post("/format-check/text", response_model=FormatCheckResponse)
async def format_check_text(
    request: FormatCheckRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check text format synchronously."""
    validate_text_size(request.text)

    try:
        result = check_format(request.text, mode=request.mode)
        return _format_response(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Format check failed: {exc}") from exc


@router.post("/format-check/file", response_model=FormatCheckResponse)
async def format_check_file(
    file: UploadFile = File(...),
    mode: str = Query("loose", description="Check mode: loose or strict"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check file format synchronously."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename cannot be empty")

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

    try:
        result = check_format(text, mode=mode)
        return _format_response(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Format check failed: {exc}") from exc


def _format_response(result) -> FormatCheckResponse:
    return FormatCheckResponse(
        success=result.success,
        is_valid=result.is_valid,
        mode=result.mode.value,
        issues=[
            FormatIssueResponse(
                line=issue.line,
                paragraph_index=issue.paragraph_index,
                issue_type=issue.issue_type.value,
                severity=issue.severity.value,
                message=issue.message,
                suggestion=issue.suggestion,
                content_preview=issue.content_preview,
            )
            for issue in result.issues
        ],
        paragraphs=[
            FormatParagraphResponse(
                index=p.index,
                text=p.text,
                line_start=p.line_start,
                line_end=p.line_end,
                paragraph_type=p.paragraph_type,
                confidence=p.confidence,
                is_auto_detected=p.is_auto_detected,
            )
            for p in result.paragraphs
        ],
        marked_text=result.marked_text,
        type_statistics=result.type_statistics,
        original_hash=result.original_hash,
        error=result.error,
    )


__all__ = [
    "format_check_file",
    "format_check_text",
    "get_paragraph_types",
    "router",
]
