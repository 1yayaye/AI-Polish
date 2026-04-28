"""Request and response schemas for the word formatter API."""

from typing import List, Optional

from pydantic import BaseModel, Field


class FormatRequest(BaseModel):
    """Request for document formatting."""

    text: Optional[str] = None
    input_format: str = "auto"
    spec_name: Optional[str] = None
    custom_spec_json: Optional[str] = None
    include_cover: bool = True
    include_toc: bool = True
    toc_title: str = "目 录"


class FormatFileRequest(BaseModel):
    """Request for file upload formatting."""

    input_format: str = "auto"
    spec_name: Optional[str] = None
    custom_spec_json: Optional[str] = None
    include_cover: bool = True
    include_toc: bool = True
    toc_title: str = "目 录"


class GenerateSpecRequest(BaseModel):
    """Request to generate spec from requirements."""

    requirements: str = Field(..., min_length=10, description="User's formatting requirements")


class JobResponse(BaseModel):
    """Response for job creation."""

    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    """Response for job status."""

    job_id: str
    status: str
    progress: Optional[float] = None
    phase: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None
    output_filename: Optional[str] = None


class SpecListResponse(BaseModel):
    """Response for listing specs."""

    specs: List[str]


class SpecSchemaResponse(BaseModel):
    """Response for spec schema."""

    schema: dict


class UsageInfoResponse(BaseModel):
    """Response for user usage info."""

    usage_count: int
    usage_limit: int
    remaining: int


class PreprocessRequest(BaseModel):
    """Request for text preprocessing."""

    text: str = Field(..., min_length=10, description="原始文章文本")
    chunk_paragraphs: int = Field(40, ge=10, le=100, description="每块最大段落数")
    chunk_chars: int = Field(8000, ge=2000, le=15000, description="每块最大字符数")


class PreprocessJobResponse(BaseModel):
    """Response for preprocess job creation."""

    job_id: str
    status: str
    message: str


class PreprocessProgressEvent(BaseModel):
    """SSE progress event for preprocessing."""

    phase: str
    total_paragraphs: int
    processed_paragraphs: int
    current_chunk: int
    total_chunks: int
    message: str
    error: Optional[str] = None
    is_recoverable: bool = True


class ParagraphInfoResponse(BaseModel):
    """Paragraph info in preprocess result."""

    index: int
    text: str
    paragraph_type: Optional[str] = None
    confidence: float = 0.0
    is_rule_identified: bool = False


class PreprocessResultResponse(BaseModel):
    """Response for preprocess result."""

    success: bool
    marked_text: str = ""
    paragraphs: List[ParagraphInfoResponse] = []
    type_statistics: dict = {}
    integrity_check_passed: bool = False
    warnings: List[str] = []
    error: Optional[str] = None


class FormatCheckRequest(BaseModel):
    """Request for format checking."""

    text: str = Field(..., min_length=10, description="原始文章文本")
    mode: str = Field("loose", description="检测模式: loose(宽松) 或 strict(严格)")


class FormatIssueResponse(BaseModel):
    """Format issue in check result."""

    line: int
    paragraph_index: int
    issue_type: str
    severity: str
    message: str
    suggestion: str
    content_preview: str = ""


class FormatParagraphResponse(BaseModel):
    """Paragraph info in format check result."""

    index: int
    text: str
    line_start: int
    line_end: int
    paragraph_type: str = "body"
    confidence: float = 1.0
    is_auto_detected: bool = True


class FormatCheckResponse(BaseModel):
    """Response for format check."""

    success: bool
    is_valid: bool = False
    mode: str = "loose"
    issues: List[FormatIssueResponse] = []
    paragraphs: List[FormatParagraphResponse] = []
    marked_text: str = ""
    type_statistics: dict = {}
    original_hash: str = ""
    error: Optional[str] = None


class ParagraphTypesResponse(BaseModel):
    """Response for paragraph types."""

    types: dict


class SaveSpecRequest(BaseModel):
    """Request to save a spec."""

    name: str = Field(..., min_length=1, max_length=100, description="规范名称")
    spec_json: str = Field(..., min_length=10, description="规范 JSON 内容")
    description: Optional[str] = Field(None, max_length=500, description="规范描述")


class SavedSpecResponse(BaseModel):
    """Response for a saved spec."""

    id: int
    name: str
    description: Optional[str] = None
    spec_json: str
    created_at: str
    updated_at: str


class SavedSpecListResponse(BaseModel):
    """Response for saved spec list."""

    specs: List[SavedSpecResponse]


__all__ = [
    "FormatCheckRequest",
    "FormatCheckResponse",
    "FormatFileRequest",
    "FormatIssueResponse",
    "FormatParagraphResponse",
    "FormatRequest",
    "GenerateSpecRequest",
    "JobResponse",
    "JobStatusResponse",
    "ParagraphInfoResponse",
    "ParagraphTypesResponse",
    "PreprocessJobResponse",
    "PreprocessProgressEvent",
    "PreprocessRequest",
    "PreprocessResultResponse",
    "SaveSpecRequest",
    "SavedSpecListResponse",
    "SavedSpecResponse",
    "SpecListResponse",
    "SpecSchemaResponse",
    "UsageInfoResponse",
]
