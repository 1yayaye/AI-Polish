"""文章预处理纵切（issue #35，GLOSSARY：排版规格前置环节）的唯一拥有者。

预处理是把整篇文章交给 AI 分块标记段落类型（标题/摘要/正文/参考文献等）的
前置环节，产物是"带 wf:type 标记的文本"，一键送格式化页按 marked 输入渲染。

- **创建（HTTP）**：模型接入未配置（ADR-0005）时创建即 503 明确失败——
  不产生任务行、不预留次数（预处理没有规则识别也能跑的降级创建形态）；
  配置齐备后走 :func:`app.word_jobs.create_format_job`（queued + 同事务
  reserve_usage，task_id=任务 id），分块参数（段落数/字符数）进 options JSON。
- **推进（Worker 委托）**：:func:`execute_preprocess_job` 驱动
  :class:`app.word_services.ArticlePreprocessor`，进度回调把库内的
  PreprocessProgress 译成进度列短事务（阶段/进度/分块两列）+ 提交后叫醒
  进度流（app.word_stream，与格式化任务同一协议）；LLM 慢调用期间零数据库
  事务。结果（标记文本/段落/统计/一致性结论）写数据目录 JSON（DB 只存
  文件名，与 docx 产物同一落盘纪律）；任务行与终态收场（完成/失败释放）
  归 app.word_worker——本模块只负责"把活干完"，不碰状态机。
- **结果（HTTP）**：GET /preprocess/{id}/result 从落盘 JSON 回读，404/409
  语义与下载端点同形（归属 404 不泄露存在性、未完成 409、文件缺失 404）。

分块进度流端点（GET /preprocess/{id}/stream）与任务进度流同协议，注册在
app.word_stream（协议唯一拥有者），不在本模块重复。
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Protocol

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, update
from sqlalchemy.orm import Session, sessionmaker

from app import billing, llm as app_llm, word_jobs, word_stream
from app.auth import get_current_user
from app.db import get_session, short_tx
from app.models import FormatJob, User
from app.word_services import (
    ArticlePreprocessor,
    PreprocessConfig,
    PreprocessPhase,
    PreprocessProgress,
    PreprocessResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/word-formatter")

# 进度 phase 词表的预处理段（终态 done/failed 归 app.word_jobs 词表）
PHASE_SPLITTING = "splitting"
PHASE_MARKING = "marking"
PHASE_VALIDATING = "validating"

# 分块参数边界（老版上传表单同款护栏；粘贴与上传共用）
_CHUNK_PARAGRAPHS_MIN, _CHUNK_PARAGRAPHS_MAX = 10, 100
_CHUNK_CHARS_MIN, _CHUNK_CHARS_MAX = 2000, 15000


class MarkingAI(Protocol):
    """预处理对 LLM 的全部依赖：分块标记的对话补全（与
    :class:`app.llm.PolishLLMClient.complete` 同形，测试接脚本化假客户端）。"""

    async def complete(self, messages: list[dict[str, str]]) -> str: ...


# ---- 进度翻译：PreprocessProgress → 进度列（展示态，不参与状态机） ----


def progress_step(
    progress: PreprocessProgress,
) -> tuple[str, float, int | None, int | None] | None:
    """一次预处理进度回调 → (phase, 0.0-1.0 进度, 第几块, 共几块)。

    分块两列只在标记段有值（分割/校验段没有"块"可展示）；终态返回 None
    （回调不写完成态——completed 收场由 Worker 的终态短事务唯一拥有，避免
    "进度已 100% 而任务行还在 running"的中间展示态）。
    """
    phase = progress.phase
    if phase is PreprocessPhase.SPLITTING:
        return PHASE_SPLITTING, 0.05, None, None
    if phase is PreprocessPhase.MARKING:
        ratio = (
            progress.current_chunk / progress.total_chunks if progress.total_chunks else 0.0
        )
        return (
            PHASE_MARKING,
            0.1 + 0.8 * ratio,
            progress.current_chunk or None,
            progress.total_chunks or None,
        )
    if phase is PreprocessPhase.VALIDATING:
        return PHASE_VALIDATING, 0.95, None, None
    return None


def _write_progress(
    session_factory: sessionmaker, job_id: int, step: tuple[str, float, int | None, int | None], message: str
) -> None:
    """预处理进度列短事务推进，提交后叫醒进度流（app.word_stream）。"""
    phase, ratio, chunk_index, chunk_total = step
    with short_tx(session_factory) as session:
        session.execute(
            update(FormatJob)
            .where(FormatJob.id == job_id)
            .values(
                phase=phase,
                progress=ratio,
                message=message,
                chunk_index=chunk_index,
                chunk_total=chunk_total,
                updated_at=func.current_timestamp(),
            )
        )
    word_stream.notify(job_id)


# ---- 执行：Worker 委托的预处理全流程（LLM 等待期间零数据库事务） ----


def preprocess_config_for(options: dict | None) -> PreprocessConfig:
    """format_jobs.options JSON → 预处理配置（键名即字段名；创建时已过边界
    校验，缺项回落默认值）。"""
    options = options or {}
    return PreprocessConfig(
        chunk_paragraphs=options.get("chunk_paragraphs", 40),
        chunk_chars=options.get("chunk_chars", 8000),
    )


async def execute_preprocess_job(
    session_factory: sessionmaker, *, job_id: int, text: str, options: dict | None, ai: MarkingAI
) -> str | None:
    """驱动一次完整预处理：分段 → 分块 AI 标记 → 组装 → 一致性校验。

    进度经回调以短事务落库（LLM 等待期间零事务）；成功把结果写数据目录
    JSON（:func:`result_path`）。返回 None 表示成功；返回失败原因字符串
    表示内容性失败，任务行收场（failed + 释放预留）由 Worker 统一处理。
    """
    config = preprocess_config_for(options)
    preprocessor = ArticlePreprocessor(ai, config)

    def on_progress(progress: PreprocessProgress) -> None:
        step = progress_step(progress)
        if step is not None:
            _write_progress(session_factory, job_id, step, progress.message)

    result = await preprocessor.preprocess(text, progress_callback=on_progress)
    if not result.success:
        return f"文章预处理失败：{result.error or '未知原因'}"
    try:
        await asyncio.to_thread(write_result_artifact, job_id, result)
    except Exception as exc:
        logger.exception("预处理任务 %d 结果落盘失败", job_id)
        return f"预处理结果落盘失败：{exc}"
    return None


# ---- 结果落盘：JSON 产物（DB 只存文件名，路径由 word_jobs 解析） ----


def result_path(job_id: int) -> Path:
    """预处理结果产物路径（word_output_dir 下按任务 id 命名的 JSON）。"""
    return word_jobs.artifact_path(f"{job_id}.json")


def write_result_artifact(job_id: int, result: PreprocessResult) -> None:
    """PreprocessResult → 结果 JSON 落盘（结构即 /preprocess/{id}/result 的
    响应契约，落盘与回读共用一份词汇）。"""
    payload = {
        "marked_text": result.marked_text,
        "paragraphs": [
            {
                "index": p.index,
                "text": p.text,
                "paragraph_type": p.paragraph_type,
                "confidence": p.confidence,
                "is_rule_identified": p.is_rule_identified,
            }
            for p in result.paragraphs
        ],
        "type_statistics": result.type_statistics,
        "integrity_check_passed": result.integrity_check_passed,
        "warnings": result.warnings,
    }
    path = result_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def read_result_artifact(job_id: int) -> dict | None:
    """回读结果 JSON；文件缺失（已被清理/磁盘故障）返回 None。"""
    path = result_path(job_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---- HTTP 层：创建（503/422/403 → 201）与结果回读 ----


def _require_model_configured() -> None:
    """创建前置闸门：模型接入未配置（ADR-0005）即 503 明确失败。检查先于
    一切写库——不产生任务行、不预留次数。"""
    if not app_llm.is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="模型接入未配置，文章预处理暂不可用",
        )


def _validated_chunk_params(chunk_paragraphs: int, chunk_chars: int) -> tuple[int, int]:
    """分块参数边界（老版同款护栏）；越界 ValueError 由调用方映射 422。"""
    if not _CHUNK_PARAGRAPHS_MIN <= chunk_paragraphs <= _CHUNK_PARAGRAPHS_MAX:
        raise ValueError(
            f"每块段落数须在 {_CHUNK_PARAGRAPHS_MIN}-{_CHUNK_PARAGRAPHS_MAX} 之间"
        )
    if not _CHUNK_CHARS_MIN <= chunk_chars <= _CHUNK_CHARS_MAX:
        raise ValueError(f"每块字符数须在 {_CHUNK_CHARS_MIN}-{_CHUNK_CHARS_MAX} 之间")
    return chunk_paragraphs, chunk_chars


def _preprocess_options(chunk_paragraphs: int, chunk_chars: int) -> dict:
    return {"chunk_paragraphs": chunk_paragraphs, "chunk_chars": chunk_chars}


class PreprocessTextRequest(BaseModel):
    """粘贴文本预处理请求：文本护栏与格式化共用（word_jobs.validated_text），
    分块参数决定 AI 标记的分块粒度。"""

    text: str
    chunk_paragraphs: int = Field(40)
    chunk_chars: int = Field(8000)

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return word_jobs.validated_text(value)


class PreprocessParagraphOut(BaseModel):
    """段落标记明细：段落序号、原文、AI/规则判定的类型与置信度。"""

    index: int
    text: str
    paragraph_type: str | None
    confidence: float
    is_rule_identified: bool


class PreprocessResultOut(BaseModel):
    """预处理结果响应：标记文本（一键送格式化的直接输入）、段落类型统计、
    一致性校验结论与全部降级警告。"""

    marked_text: str
    paragraphs: list[PreprocessParagraphOut]
    type_statistics: dict[str, int]
    integrity_check_passed: bool
    warnings: list[str]


def _create_preprocess_job(
    session: Session,
    user: User,
    *,
    text: str,
    chunk_paragraphs: int,
    chunk_chars: int,
    input_file_name: str | None = None,
) -> FormatJob:
    """共享创建路径：分块参数校验（422）→ 预留次数（403）→ queued。"""
    try:
        _validated_chunk_params(chunk_paragraphs, chunk_chars)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    try:
        return word_jobs.create_format_job(
            session,
            user,
            job_type=word_jobs.JOB_PREPROCESS,
            text=text,
            options=_preprocess_options(chunk_paragraphs, chunk_chars),
            input_file_name=input_file_name,
        )
    except billing.InsufficientUsage as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.post("/preprocess/text", response_model=word_jobs.JobOut, status_code=status.HTTP_201_CREATED)
def create_preprocess_text_job(
    request: PreprocessTextRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> FormatJob:
    """粘贴文本创建预处理任务：模型未配置 503（零副作用）→ 文本/分块参数
    校验 422 → 预留次数 403 → queued。"""
    _require_model_configured()
    return _create_preprocess_job(
        session,
        user,
        text=request.text,
        chunk_paragraphs=request.chunk_paragraphs,
        chunk_chars=request.chunk_chars,
    )


@router.post("/preprocess/file", response_model=word_jobs.JobOut, status_code=status.HTTP_201_CREATED)
async def create_preprocess_file_job(
    file: UploadFile,
    chunk_paragraphs: int = Form(40),
    chunk_chars: int = Form(8000),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> FormatJob:
    """上传文件创建预处理任务：解码/提文本与格式化上传共用同一护栏
    （word_jobs.extract_upload_text，422），其余同粘贴路径。"""
    _require_model_configured()
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
    return _create_preprocess_job(
        session,
        user,
        text=text,
        chunk_paragraphs=chunk_paragraphs,
        chunk_chars=chunk_chars,
        input_file_name=file_name,
    )


@router.get("/preprocess/{job_id}/result", response_model=PreprocessResultOut)
def get_preprocess_result(
    job_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> PreprocessResultOut:
    """预处理结果：标记文本、段落类型统计、一致性校验结论。

    归属失败/非预处理任务 404（不泄露他人任务存在性，任务类型错位按不存在
    处理）；未完成 409；结果文件缺失（磁盘被清理）404——库行在而文件不在，
    按不存在处理。
    """
    try:
        job = word_jobs.get_job_detail(session, user, job_id)
    except word_jobs.JobNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if job.job_type != word_jobs.JOB_PREPROCESS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="预处理结果不存在")
    if job.status != word_jobs.JOB_COMPLETED:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="任务尚未完成，暂无结果")
    payload = read_result_artifact(job_id)
    if payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="预处理结果不存在")
    return PreprocessResultOut.model_validate(payload)
