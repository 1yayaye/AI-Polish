"""格式检查纵切（issue #37，GLOSSARY：免费格式检查）的唯一拥有者。

检查是排版前的不计费诊断：纯规则（:func:`app.word_services.check_format`）、
同步完成、宽松/严格两档——与格式化/预处理的"创建任务 + 预留次数"形成刻意
对照：本模块全程不创建任务、不写账本（免费语义由 test_word_check 钉住），
因此也没有进度流与结果落盘——请求即响应的终态。

- **字典**：GET /format-check/types 返回检查器段落类型词表与说明（页面
  字典说明与检查结果术语的唯一数据源，词汇不在此复制第二份）。
- **检查（HTTP）**：POST /format-check/text（粘贴）与 /format-check/file
  （上传）。输入护栏与格式化/预处理共用同一份（word_jobs.validated_text 与
  extract_upload_text，422 挡在检查之前）；mode 越出两档由 pydantic 422。

卡密只经 X-Card-Key header（app.auth），与其他 word-formatter 端点同约定。
"""

from enum import Enum

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app import word_jobs
from app.auth import get_current_user
from app.db import get_session
from app.models import User
from app.word_services import PARAGRAPH_TYPES, check_format

router = APIRouter(prefix="/api/word-formatter")


class CheckModeName(str, Enum):
    """检查档位（检查器两档的 HTTP 词汇，宽松为默认）。"""

    loose = "loose"
    strict = "strict"


class ParagraphTypeOut(BaseModel):
    """段落类型字典条目：类型名即检查结果/标记文本里的词汇，label 是中文说明。"""

    type: str
    label: str


class FormatIssueOut(BaseModel):
    """单条格式问题：行号定位 + 严重度分级 + 问题类型（页面按行号排明细）。"""

    line: int
    paragraph_index: int
    issue_type: str
    severity: str
    message: str
    suggestion: str
    content_preview: str


class FormatCheckOut(BaseModel):
    """检查响应：is_valid 以 ERROR 级问题为准（严格档才有非 ERROR 级可看）。"""

    is_valid: bool
    mode: str
    issues: list[FormatIssueOut]
    type_statistics: dict[str, int]


class CheckTextRequest(BaseModel):
    """粘贴检查请求：文本护栏与格式化共用（word_jobs.validated_text）。"""

    text: str
    mode: CheckModeName = Field(CheckModeName.loose)

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        try:
            return word_jobs.validated_text(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


@router.get("/format-check/types")
def list_paragraph_types(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict[str, list[ParagraphTypeOut]]:
    """段落类型字典与说明（检查结果的术语表，页面的字典说明数据源）。"""
    return {
        "types": [
            ParagraphTypeOut(type=name, label=label) for name, label in PARAGRAPH_TYPES.items()
        ]
    }


@router.post("/format-check/text", response_model=FormatCheckOut)
def check_format_text(
    request: CheckTextRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> FormatCheckOut:
    """粘贴检查：文本校验（422）→ 同步规则检查。零任务行、零账本行（免费）。"""
    return _run_check(request.text, request.mode)


@router.post("/format-check/file", response_model=FormatCheckOut)
async def check_format_file(
    file: UploadFile,
    mode: CheckModeName = Form(CheckModeName.loose),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> FormatCheckOut:
    """上传检查：解码/提文本与格式化上传共用同一护栏（422），其余同粘贴路径。"""
    file_name = file.filename or ""
    content = await file.read()
    try:
        text, _ = word_jobs.extract_upload_text(file_name, content)
    except word_jobs.UploadFormatError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    try:
        text = word_jobs.validated_text(text)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return _run_check(text, mode)


def _run_check(text: str, mode: CheckModeName) -> FormatCheckOut:
    """同步执行一次检查并译成响应（不入库、不碰计费——免费语义在此收口）。"""
    result = check_format(text, mode=mode.value)
    return FormatCheckOut(
        is_valid=result.is_valid,
        mode=result.mode.value,
        issues=[
            FormatIssueOut(
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
        type_statistics=result.type_statistics,
    )
